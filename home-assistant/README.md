# Home Assistant – N.MUSIC (B&O Masterlink via MDTv2-Pi)

HA-Bausteine für die Masterlink-Quelle N.MUSIC: Music Assistant (Sendspin) auf dem
MDT-Pi, Beo4-Steuerung und Dashboard-Anzeige.

| Ordner | Inhalt |
|---|---|
| `automations/` | Automatisierungen (je Datei eine, im Format des HA-YAML-Editors) |
| `scripts/` | Skripte (im Format des HA-YAML-Editors) |
| `templates/` | Zustandstemplates der Template-Sensoren (`.jinja`) und der Webhook-Sensor `sensor.mdt_n_music` |
| `dashboard/` | button-card für das Dashboard (benötigt `custom:button-card` aus HACS) |
| `helpers/` | Referenz der Helfer (nur Doku) |

Doku: [`docs/HA_CONFIG.md`](../docs/HA_CONFIG.md) (Zweck und Zusammenspiel der Bausteine),
[`docs/HA_INTEGRATION.md`](../docs/HA_INTEGRATION.md) (Anbindung Pi → HA).

## Verwendung

Alles wurde über die HA-Oberfläche angelegt. Zum Übernehmen jeweils in HA die
Automatisierung / das Skript bzw. die Karte öffnen → „In YAML bearbeiten“ → Inhalt einfügen.
Die `.jinja`-Dateien sind die Zustandstemplates der Template-Sensoren
(Helfer → Template → Template-Sensor).

`radio_liste_befuellen_default_snippet.yaml` ist kein vollständiges Skript, sondern der
Block, der in der bestehenden Befüllungs-Automatisierung nach `input_select.set_options` steht.

Platzhalter sind mit `<…>` markiert.

## Streams, die nicht von Music Assistant kommen (AirPlay & Co.)

Damit die Karte auch AirPlay direkt vom iPhone anzeigt, meldet der Pi per Webhook,
welcher Provider auf N.MUSIC spielt:

1. `secrets.yaml`: `mdt_nmusic_webhook: mdt-nmusic-<langer-zufallswert>`
   (z. B. `openssl rand -hex 16`).
2. Inhalt von `templates/mdt_n_music_webhook.yaml` in `configuration.yaml` übernehmen
   (bzw. in einen vorhandenen `template:`-Block einfügen), HA neu starten.
3. Template-Sensoren `n_music_quelle` / `n_music_inhalt` und die Karte mit den
   aktuellen Dateien aktualisieren.
4. Auf dem Pi in `/etc/ml-source-bridge.toml`:
   ```toml
   [ha_notifier]
   enabled = true
   url     = "http://<ha>:8123/api/webhook/mdt-nmusic-<langer-zufallswert>"
   sources = ["N.MUSIC"]
   ```
   dann `sudo systemctl restart ha-notifier.service`.

`local_only: true` am Webhook: HA nimmt den POST nur aus dem lokalen Netz an.
