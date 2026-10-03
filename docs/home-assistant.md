# Home Assistant integration

Home Assistant (HA) building blocks for the sources the Pi feeds -- here
**N.MUSIC** with Music Assistant (via Sendspin) and AirPlay sharing it, and
a **turntable** (Beogram 7000, source label "BG7000") with optional music
recognition. The configuration itself is in
[`home-assistant/`](../home-assistant/); this page explains how the pieces
fit and how to set them up.

What you get:

- One dashboard card for the system: what N.MUSIC plays -- source, station
  or playlist, title, artist, cover -- with prev / play-pause / next.
- It shows streams that **don't** come from Music Assistant too, e.g.
  AirPlay straight from an iPhone, reported by the Pi.
- While the turntable plays it shows the record instead: title, artist,
  album and cover once music recognition has identified the track, else
  "Schallplatte" in front of a default image (a placeholder drawing, or a
  photo of your own deck).
- Beo4 control: `N.MUSIC` + digit picks a radio station, + colour key starts
  a playlist.

## How it fits together

```
Music Assistant ──Sendspin──►┐
iPhone ──────────AirPlay────►├─ Pi: ml-source-bridge ── redis ── ha-notifier ──webhook──► sensor.mdt_n_music
                             │       (now playing)                (cover on :8099)              │
                             └──────────────► Masterlink N.MUSIC                                │
media_player.masterlink_bridge_ma (MA's view) ─────────────────────────────────┐                │
                                                                               ▼                ▼
                                             sensor.n_music_quelle / sensor.n_music_inhalt ──► button-card
```

- While **Music Assistant** plays, the card uses the MA player entity: it has
  the richest metadata (station names, queue, cover).
- While **another provider** plays (`sensor.mdt_n_music` reports a provider
  other than `sendspin`), the card uses what the Pi reports instead.

## Requirements

- Music Assistant with the Pi's Sendspin client as a player -- here
  `media_player.masterlink_bridge_ma`. Adjust the entity id in the files if
  yours differs.
- [`custom:button-card`](https://github.com/custom-cards/button-card) from
  HACS.
- For the Beo4 automations: the MLGW integration, which fires
  `mlgw.ML_telegram` events (`payload_type: BEO4_KEY`, `payload.source`,
  `payload.command`).
- HA opened over **plain http in the LAN** if you want AirPlay cover art:
  the browser loads it straight from the Pi, and an https page would block
  that as mixed content.
- On the Pi: ha-notifier (installed by `install.sh`).

## Entities

| Entity | Type | Purpose |
|---|---|---|
| `media_player.masterlink_bridge_ma` | MA player | Music Assistant -> Pi (Sendspin) -> N.MUSIC |
| `sensor.mdt_n_music` | trigger template sensor (webhook) | What the Pi reports for N.MUSIC: provider, state, title / artist / album, cover URL |
| `sensor.mdt_phono` | trigger template sensor (same webhook) | The turntable: state, and title / artist / album / cover once recognized |
| `sensor.n_music_quelle` | template sensor | Source label: `Internet Radio` / `Tidal` / `Music Assistant` / `Extern` / provider name from the Pi (e.g. `Apple Music`) / `–` |
| `sensor.n_music_inhalt` | template sensor | Station or playlist name; album for non-MA streams; `–` |
| `input_select.radio_station_list` | helper | Radio stations = MA favourites, filled by an automation |
| `input_text.radio_default_station` | helper | Station selected after a restart |
| `input_text.n_music_inhalt` | helper (max 255) | Name of the playlist started via Beo4 (MA doesn't expose it) |
| `script.radio_station_wahlen` | script | Select a station, or restart it if already selected |
| `script.n_music_vor_zurueck` | script | Prev/next: station for radio, track otherwise |

The German names (`quelle` = source, `inhalt` = content, `wählen` = select,
`vor/zurück` = next/previous) are the entity ids of the original setup; keep
or rename them consistently across all files.

## Setup

### 1. Pi: enable ha-notifier

Create a webhook id (it acts like a password -- anyone who knows it can set
the sensor from your LAN):

```sh
echo "mdt-nmusic-$(openssl rand -hex 16)"
```

Add to `/etc/ml-source-bridge.toml`:

```toml
[ha_notifier]
enabled = true
url     = "http://<ha-host>:8123/api/webhook/mdt-nmusic-<random>"
sources = ["N.MUSIC", "BG7000"]   # each has its own sensor in HA
```

```sh
sudo systemctl restart ha-notifier
sudo journalctl -u ha-notifier -f
```

All options (debounce, keepalive, cover port, …) are documented in
[`config.toml.example`](../ml-source-bridge/config.toml.example) and
[`ha-notifier/README.md`](../ha-notifier/README.md). If `<hostname>.local`
doesn't resolve on the devices showing the dashboard, set
`cover_base_url = "http://<pi-ip>:8099"`.

### 2. HA: webhook sensor

`secrets.yaml` (next to `configuration.yaml`):

```yaml
mdt_nmusic_webhook: mdt-nmusic-<random>     # the same id as on the Pi
```

Copy [`templates/mdt_webhook.yaml`](../home-assistant/templates/mdt_webhook.yaml)
into `configuration.yaml` -- trigger-based template sensors can't be created
in the UI. If `configuration.yaml` already has a `template:` key, add the
`- trigger:` list entry under it instead of a second `template:`.

It defines one sensor per source on the same webhook; each takes only the
messages for its source (`"source"` = the source's `display_name` on the
Pi) and keeps its state otherwise. If your turntable source isn't called
`BG7000`, change it in the `MDT Phono` sensor and in `sources` on the Pi.

*Developer tools → YAML → Check configuration*, then restart HA (later
changes: reload *Template entities*). `sensor.mdt_n_music` appears with state
`unknown` until the first POST.

**Test without the Pi:**

```sh
curl -X POST -H 'Content-Type: application/json' \
  -d '{"source":"N.MUSIC","provider":"airplay","display":"Apple Music","state":"playing","title":"Test","artist":"Artist","album":"Album","cover_url":""}' \
  http://<ha-host>:8123/api/webhook/mdt-nmusic-<random>
```

HA answers 200 even for an unknown id -- check the sensor's state instead.

### 3. Helpers

Create in *Settings → Devices & services → Helpers*, as listed in
[`helpers/helpers.yaml`](../home-assistant/helpers/helpers.yaml) (reference
only -- don't load that file as well, or the entities exist twice):
`input_text.radio_default_station`, `input_text.n_music_inhalt` (max 255) and
`input_select.radio_station_list`.

### 4. Template sensors

*Helpers → Create helper → Template → Template sensor* (no unit, device or
state class), with the state template from:

- `sensor.n_music_quelle` ← [`templates/n_music_quelle.jinja`](../home-assistant/templates/n_music_quelle.jinja)
- `sensor.n_music_inhalt` ← [`templates/n_music_inhalt.jinja`](../home-assistant/templates/n_music_inhalt.jinja)

Both check `sensor.mdt_n_music` first: if the Pi reports a provider other
than Sendspin playing, they show its display name (`Apple Music`) and the
album. Otherwise they derive the label from the MA player:
`media_content_id` `library://radio/…` = Internet Radio (content = station,
which MA puts in `media_album_name`), a playlist name in
`input_text.n_music_inhalt` = Tidal, a source other than
`Music Assistant Queue` = Extern.

### 5. Scripts and automations

Paste each file via *New script / New automation → ⋮ → Edit in YAML*:

| File | What it does |
|---|---|
| [`scripts/radio_station_wahlen.yaml`](../home-assistant/scripts/radio_station_wahlen.yaml) | Station already selected → play it; else only change the selection and let *change Radio Station* play it. Never starts twice. |
| [`scripts/n_music_vor_zurueck.yaml`](../home-assistant/scripts/n_music_vor_zurueck.yaml) | Radio: next/previous station (cycling). Otherwise next/previous track. |
| [`automations/n_music_change_radio_station.yaml`](../home-assistant/automations/n_music_change_radio_station.yaml) | Plays the selected station -- only on real selection changes, not at HA start, attribute updates or while the list is being refilled. |
| [`automations/n_music_radio_beo4_ziffer.yaml`](../home-assistant/automations/n_music_radio_beo4_ziffer.yaml) | Beo4 `N.MUSIC` + digit → station (map in `variables.stationen`). |
| [`automations/n_music_tidal_beo4_farbtaste.yaml`](../home-assistant/automations/n_music_tidal_beo4_farbtaste.yaml) | Beo4 `N.MUSIC` + colour key → playlist, shuffled (map in `variables.playlists`); remembers its name for the card. |
| [`automations/n_music_playlist_info_reset.yaml`](../home-assistant/automations/n_music_playlist_info_reset.yaml) | Clears the playlist name after 2 min of not playing or on a station change. |
| [`automations/masterlink_default_lautstaerke.yaml`](../home-assistant/automations/masterlink_default_lautstaerke.yaml) | Sets the MA player to 30 % when it starts from idle/off. |
| [`automations/radio_liste_befuellen_default_snippet.yaml`](../home-assistant/automations/radio_liste_befuellen_default_snippet.yaml) | Not a full automation: the block to add to your station-list fill automation, right after its `input_select.set_options`, to select the default station. |

Placeholders are marked `<…>` (stations, playlists, the id of your fill
automation in *change Radio Station*).

### 6. Dashboard card

Copy [`www/mdt/turntable.svg`](../home-assistant/www/mdt/turntable.svg) to
`/config/www/mdt/` on HA (served as `/local/mdt/turntable.svg`) -- the
turntable's default image. Then add a card, *Show code editor*, paste
[`dashboard/n_music_button_card.yaml`](../home-assistant/dashboard/n_music_button_card.yaml).

Settings at the top of the card, under `variables`:

| Variable | Default | Meaning |
|---|---|---|
| `phono_image` | `/local/mdt/turntable.svg` | Turntable background while no track is recognized -- e.g. a photo of your own deck in `/config/www/mdt/` (`/local/mdt/bg7000.jpg`) or any URL |
| `phono_label` | `BG7000` | Header label for the turntable |

- Visible while N.MUSIC plays (`sensor.n_music_quelle` isn't `–`) or the
  turntable plays (in edit mode it always shows).
- **N.MUSIC:** header *N.MUSIC · source · content*, title, artist, cover as
  background. For non-MA streams, title, artist and cover come from
  `sensor.mdt_n_music`.
- **Turntable:** header *BG7000 · album*, title, artist, cover -- or
  "Schallplatte" and the default image while nothing is recognized.
- Buttons only for Music Assistant; HA can't control the other providers
  or the turntable.

## Behaviour notes

- **Pause hides the card**, for MA and AirPlay alike -- the sensors only
  count `playing`.
- **Idle:** when an AirPlay client disconnects (or MA stops), the Pi sends
  `idle` and the card disappears. ha-notifier waits 3 s before sending a
  change away from `playing`, so the brief pause shairport-sync reports while
  the ML system wakes up doesn't make the card flicker.
- **HA restarts:** trigger sensors keep their last state; ha-notifier resends
  the current state every 5 minutes.
- **Turntable:** titles need `[turntable] recognize = true` on the Pi (see
  the bridge README); without it the card shows the default image whenever
  the turntable plays.
- **Payload** the Pi sends:
  `{"source", "source_byte", "provider", "display", "state", "title", "artist", "album", "cover_url"}`,
  `state` = `playing` / `paused` / `idle`.

## Troubleshooting

| Symptom | Check |
|---|---|
| `sensor.mdt_n_music` never changes | `journalctl -u ha-notifier`: POST errors? Webhook id identical on both sides? `sources` matches the source's `display_name`? |
| Sensor right, card missing | State of `sensor.n_music_quelle` -- still the old template? In *Developer tools → Template* paste the `.jinja` file to see what it yields. |
| No cover for AirPlay | Open `cover_url` from the sensor in a browser. Not loading: `.local` name not resolving (set `cover_base_url`), or `mdt` can't read shairport-sync's cover cache (see `journalctl -u ha-notifier`). HA opened via https: blocked as mixed content. |
| Nothing on redis | `redis-cli SUBSCRIBE link:ml:nowplaying` on the Pi; see [providers.md](providers.md). |

## Not covered (yet)

- Transport control (play/pause/skip) for AirPlay from HA -- the bridge could
  drive shairport-sync, but there's no path from HA to it.
- An ESP32 display (Guition ESP32-S3-4848S040, ESPHome/LVGL) reading the same
  sensors is planned.
