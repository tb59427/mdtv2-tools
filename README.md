# TB version of mdtv2-tools

A fork of Philip Voigt's great Masterlink toolset MDTV2
(<https://gitlab.com/masterdatatool/software/mdtv2-tools>). On top of
Philip's work it adds:

- **Sendspin** (e.g. Music Assistant) and **MPD** as audio providers
- **several providers per B&O source** -- e.g. Sendspin and AirPlay sharing
  N.MUSIC, whoever starts last plays
- **Music recognition** via Shazam (shazamio) from the audio alone -- title,
  artist, album and cover: for the turntable (`recognize = true`), and for
  whatever else the ML bus carries, e.g. a CD on the BeoSound
  (`[ml_listen]`); both optional
- **Home Assistant integration**: the Pi reports what each source plays
  (including AirPlay straight from an iPhone, with cover art, and the
  recognized record or CD), plus HA automations and one dashboard card for it all
- **Web UI**: configure sources, providers, turntable, Home Assistant and
  LIGHT keys in a browser -- validated by the bridge, comments in the config
  kept
- **MasterLink Gateway emulation**: the Pi can replace a B&O MLGW for Home
  Assistant's mlgw integration -- devices, Beo4 commands, ML events

> [!WARNING]
> **Beta:** the **MasterLink Gateway emulation**, the **web UI** and the
> extended **`install.sh`** are in beta. They run in daily use on the
> author's system -- where the Pi has replaced a real MLGW for Home
> Assistant after side-by-side tests against it, and `install.sh` updated
> the installation -- but that is one system. Anyone who wants to try them
> is very welcome -- but if something breaks, you may well be on your own.
> Keep a way back (see [testing a checkout on the Pi](docs/testing.md)) and
> a copy of your `/etc/ml-source-bridge.toml`.

The full list of changes is in [CHANGES.md](CHANGES.md).

**Docs for the additions:**
[providers](docs/providers.md) (AirPlay, Sendspin, MPD, ALSA) ·
[Home Assistant](docs/home-assistant.md) ·
[web UI](docs/web-ui.md) ·
[MasterLink Gateway emulation](docs/mlgw-emulation.md) ·
[testing a checkout on the Pi](docs/testing.md)

`install.sh` sets up everything except Sendspin and MPD, which are installed
by hand as described in [docs/providers.md](docs/providers.md).

# mdtv2-tools

Python tool-set for the MasterDataTool v2. A Raspberry Pi acessory board that interfaces vintage B&O devices like BeoSound 9000, BeoCenter 2, BeoLab 3500, BeoGram 7000, BeoCenter 9500, etc. and makes them compatible with the modern streaming world.

Receive and send any remote control messages or analog audio streams via MasterLink or DataLink to your classic audio system. Converts your Raspberry into a ML or DL device 

[https://labs.polyvection.com/mdt](https://labs.polyvection.com/mdt)

## Features at a glance

| Feature | What it does | Lives in |
|---|---|---|
| **Source Center emulation** | Pi appears on the ML bus as an SC, claims a configurable source byte (e.g. N.RADIO), pipes shairport-sync audio onto the bus with track / artist metadata. Lets use use the NET sources of ML audio masters| `ml-source-bridge/` (`role = "sc"`) |
| **Audio Master emulation** | Pi pretends to be an Audio Master so ML link-speakers in setups without a real BeoMaster can play sources we provide. | `ml-source-bridge/` (`role = "am"`) |
| **LIGHT-key home automation** | Beo4 remote `LIGHT + <any key>` runs a user-configured shell command (Home Assistant call, MQTT publish, GPIO toggle, whatever). Not all ML devices can forward them - check if yours supports it beforehand | `ml-source-bridge/` (`[light_handler]`) |
| **DL'80 turntable control** | Remote control your DL enabled BeoGram turntable | `dl-docs/dl80-beogram/`, `dl-scripts/dl80-turntable/` |
| **Turntable as an ML source** | Play a DL'80 Beogram *through* your ML music system: pick the source on your Beo remote to start the record, Step Up/Down to skip tracks, and the analogue audio is looped ADC→DAC onto ML. Opt-in per source (typically N.RADIO), Feed it line level (its own preamp, or an external phono stage); the software RIAA + high ADC gain option is a dev-only path with poor sound quality. | `ml-source-bridge/` (`provider = "turntable"`) |
| **Music recognition (turntable)** | Optional: identifies the record playing on the turntable source via Shazam (shazamio, unofficial client) from the audio alone -- a new track is detected from the silent gap -- and shows title / artist / album / cover on panels that display source texts and in Home Assistant. A result is shown only when two attempts agree. Nothing is written to disk. | `ml-source-bridge/` (`[turntable] recognize = true`) |
| **Music recognition (bus sources)** | Optional: the HAT's ADC also hears the MasterLink audio lines, so while the audio master plays a source the Pi doesn't provide (CD, A.MEM, ...), the bridge listens along and identifies the music the same way -- title / artist / album / cover for Home Assistant. A new CD track number starts a new recognition. Shares the ADC with the turntable, which has priority. | `ml-source-bridge/` (`[ml_listen]`) |
| **DL'86 music-system control** | Remote control your DL enabled BeoCenter or BeoMaster turntable | `dl-docs/dl86-music-system/`, `dl-scripts/dl86-system/` |
| **Phono capture (turntable)** | Sample script to do a 60 s record from a turntable into FLAC: triggers `BG.Play`, configures the ADC, records and optionally applies a software RIAA de-emphasis. | `dl-scripts/dl80-turntable/` |
| **CD capture (music system)** | Sample script to do a 60 s record from a CD source: triggers DL'86 CD on, configures ADC, records, encodes FLAC and sends standby. | `dl-scripts/dl86-system/` |
| **MasterLink protocol debugger** | Pretty-prints every ML telegram on the bus, decoded (TO/FROM/PT/payload). Compact and verbose modes. | `ml-debug/` |
| **Datalink protocol debugger** | Pretty-prints every DL'80 and DL'86 message, decoded (opcode names for DL'80; address/format/payload breakdown for DL'86 including status frames with volume/track/standby semantics). | `dl-debug/` |
| **Bus state variables** | Daemon that keeps the current ML / DL'80 / DL'86 status (active source, transport, track, volume) in Redis keys `state:ml` / `state:dl80` / `state:dl86`, and publishes a change event on `link:<bus>:state`. Read with one `redis-cli GET`. | `state-tracker/` |
| **ML device discovery** | The same daemon sweeps the MasterLink bus with `MASTER_PRESENT` pings and keeps a live inventory of which addresses/devices are present (AM / VM / SC / link nodes, with class) in `state:ml:devices`. Runs at startup and on demand (`PUBLISH link:ml:discover`). Non-disruptive — doesn't interrupt playback. | `state-tracker/` |
| **Web UI** | Browser configuration of the bridge (sources, multi-provider, turntable incl. recognition, HA notifier, LIGHT keys) and the MLGW emulation. Saving lets the bridge validate the config first, keeps a backup and the file's comments, and restarts only what changed. Opt-in (`[web]`). | `web/` |
| **MasterLink Gateway emulation** | The Pi answers Home Assistant's mlgw integration like a B&O MasterLink Gateway: device list, Beo4 commands onto the bus, `mlgw.ML_telegram` events from every telegram, and the MLGW's own events (LIGHT/CONTROL, all standby, source and picture/sound status) -- checked side by side against a real MLGW. Opt-in (`[mlgw]`). | `mlgw-emu/` |
| **Home Assistant now-playing** | The bridge publishes per source which provider is playing and what (`state:nowplaying` / `link:ml:nowplaying` on Redis); `ha-notifier` forwards every change to an HA webhook, so HA shows AirPlay & co. too, not just Music Assistant. HA config (sensors, templates, button-card) included. | `ha-notifier/`, `home-assistant/` |
| **One-line install** | `curl … bootstrap.sh \| sudo bash` on a fresh Pi OS Lite installs apt deps, patches `config.txt`, sets up systemd services, deploys all code, builds a hidden pymcuprog venv for flashing. | `bootstrap.sh`, `install.sh` |
| **MCU updater** | `sudo flash.sh firmware-vX.Y.Z.hex` lets you update the microcontroller handling raw ML and DL communication | `mcu-firmware/flash.sh` |

## Architecture

```
       Beo4 remote                   Beogram / CD player          AirPlay client
            │                              │                            │
            ▼                              ▼                            ▼
       MasterLink bus  ◄────────────►   Datalink bus            shairport-sync
            ▲                              ▲                            │
            │                              │                            │
       ╔════╧══════════════════════════════╧═════════════╗              │
       ║         MDT HAT  (ml / dl-80 / dl-86)           ║              │
       ║         ↕ /dev/serial0 + GPIO + I²S             ║              │
       ╚═════════════════════════╤═══════════════════════╝              │
                                 │                                      │
                                 ▼                                      │
                          mdtv2-broker          ─────► Redis pub/sub ◄──┘
                                                            │
                                                            ▼
                          ml-source-bridge / state-tracker / dl-debug / ml-debug
                                    (Python services + tools)
```

`mdtv2-broker` is the only thing that talks to the MCU. Everything else
talks to Redis channels (`link:ml:*`, `link:dl80:*`, `link:dl86:*`,
`link:gpio:*`), so you can drop in extra subscribers without touching
the broker.

## Quick start

On a fresh Raspberry Pi OS Lite (Bookworm or later), one line:

```sh
curl -sSL https://raw.githubusercontent.com/tb59427/mdtv2-tools/master/bootstrap.sh | sudo bash
```

That installs git, clones the repo into `/opt/mdt-tools-src/`, then
runs `install.sh` which handles everything else (apt deps, boot config
patch for the DAC+ADC overlay + UART, systemd units, hidden pymcuprog
venv). The installer may say `REBOOT REQUIRED` if it had to change
`/boot/firmware/config.txt` — if so, `sudo reboot`.

If you'd rather inspect the script before piping it to bash:

```sh
curl -sSL https://raw.githubusercontent.com/tb59427/mdtv2-tools/master/bootstrap.sh
```

Or the manual two-step:

```sh
git clone https://github.com/tb59427/mdtv2-tools.git
cd mdtv2-tools
sudo ./install.sh
```

After the install (and reboot if needed):

```sh
# 1. configure the bridge (sources, role, light_handler, ...)
sudo nano /etc/ml-source-bridge.toml

# 2. enable the services (install.sh already does this on first run)
sudo systemctl enable --now mdtv2-broker.service ml-source-bridge.service

# 3. follow logs
sudo journalctl -u mdtv2-broker.service -u ml-source-bridge.service -f

# 4. flash the MCU (optional, comes pre-flashed; install.sh leaves the .hex in place)
sudo /opt/mdt-tools/mcu-firmware/flash.sh \
     /opt/mdt-tools/mcu-firmware/firmware-v1.5.5.hex

# 5. inspect bus traffic
python3 /opt/mdt-tools/ml-debug/ml_debug.py        # MasterLink
python3 /opt/mdt-tools/dl-debug/dl_debug.py        # Datalink
```

## Configuration

Everything lives in `/etc/ml-source-bridge.toml` (copied from
[`ml-source-bridge/config.toml.example`](ml-source-bridge/config.toml.example)
on first install -- that file documents every option and the Beo4 key
names). After editing:

```sh
sudo systemctl restart ml-source-bridge ha-notifier
```

### Role and sources

```toml
role = "sc"                  # "sc" next to a B&O system, "am" to be its audio master

[[sources]]                  # one table per ML source byte we claim
source_byte  = 0xA1          # N.RADIO (also: 0x7A N.MUSIC, 0x8D CD, 0x6F RADIO)
provider     = "airplay"     # airplay | sendspin | mpd | turntable
display_name = "N.RADIO"     # shown on B&O panels (keep it short)
```

### Several providers on one source (multi-stream)

`provider` can be a list. Whoever **starts streaming last** owns the source:
the bridge pauses the previous one, so only one stream reaches the bus.

```toml
[[sources]]
source_byte      = 0x7A                          # N.MUSIC
provider         = ["sendspin", "airplay", "mpd"]
provider_default = "sendspin"   # gets Beo4 PLAY when nothing is playing (default: first)
display_name     = "N.MUSIC"    # shown while the source is idle

[provider_displays]             # label per provider while it plays
airplay  = "Apple Music"
sendspin = "Music Assistant"
mpd      = "MPD Stream"
```

- Next/previous go to the provider that currently plays; pause goes to all.
- All providers must play through ALSA `dmix` so the handover works --
  shairport-sync needs `output_device = "plug:dmix"`. Setup of AirPlay,
  Sendspin and MPD: [docs/providers.md](docs/providers.md).

MPD defaults to `localhost:6600` without password; override in `[mpd]`:

```toml
[mpd]
host     = "localhost"
port     = 6600
password = ""
```

### Turntable and music recognition

A DL'80 Beogram as a source; its audio is looped from the HAT's ADC to the
DAC. Feed it line level (deck preamp or external phono stage).

```toml
[[sources]]
source_byte  = 0xA1
provider     = "turntable"
display_name = "BG7000"

[turntable]
metadata_title = "BG7000"   # panel text while nothing is recognized
recognize      = true       # identify the record via Shazam (optional)
```

`recognize = true` identifies each track from the audio alone (no Datalink
needed) and shows title / artist / album / cover -- on panels that display
source texts, and in Home Assistant. It uses shazamio, an unofficial Shazam
client, which `install.sh` installs into its own venv only when this is set,
so **re-run `install.sh` after enabling it**. Details:
[bridge README](ml-source-bridge/README.md#music-recognition-optional).

### Music recognition on the bus

Recognizes what *other* sources play -- a CD on the BeoSound, A.MEM, radio
-- by listening to the MasterLink audio lines through the HAT's ADC:

```toml
[ml_listen]
enabled = true
# sources = ["CD", "A.MEM"]   # omit = every source the Pi doesn't provide

[ha_notifier]
sources = ["N.MUSIC", "BG7000", "ml_listen"]   # "ml_listen" = all of them
```

Results go to Home Assistant (`sensor.mdt_ml_listen`, shown on the same
card). Same shazamio venv as the turntable -- **re-run `install.sh` after
enabling**. Details:
[bridge README](ml-source-bridge/README.md#music-recognition-on-the-bus-ml_listen).

### Home Assistant

```toml
[ha_notifier]
enabled = true
url     = "http://<ha-host>:8123/api/webhook/<webhook-id>"
sources = ["N.MUSIC", "BG7000"]     # display_names to report
```

ha-notifier posts every change (provider, title, artist, album, cover) to
the HA webhook; HA config and setup: [docs/home-assistant.md](docs/home-assistant.md).

### LIGHT key (home automation hook)

Beo4 `LIGHT` + any key runs a shell command:

```toml
[light_handler]
enabled   = true
timeout_s = 20

[light_handler.commands]
"digit_1"   = "curl ... # turn evening scene on"
"digit_2"   = "/usr/local/bin/lights-off.sh"
"step_up"   = "/usr/local/bin/lights-brighter.sh"
"step_down" = "/usr/local/bin/lights-dimmer.sh"
"red"       = "/usr/local/bin/movie-mode.sh"
```

### Web UI and MasterLink Gateway emulation

```toml
[web]
enabled  = true
username = "admin"
password = "<choose one>"       # then: sudo systemctl restart mdt-web
```

Everything above can then be set at `http://<pi>/`; the MLGW emulation
(`[mlgw]`) is switched on there too. Guides: [docs/web-ui.md](docs/web-ui.md),
[docs/mlgw-emulation.md](docs/mlgw-emulation.md).

The config file is `root:mdt 0660` -- it can hold the webhook URL and
tokens in commands; group-writable so the web UI can save it.

## Repo layout vs. installed layout

`install.sh` copies code into `/opt/mdt-tools/`, mirroring the repo:

```
/opt/mdt-tools/
├── broker/
├── ml-source-bridge/
├── ml-debug/
├── dl-debug/
├── dl-scripts/
├── state-tracker/         # state:ml / state:dl80 / state:dl86 daemon
├── ha-notifier/           # now-playing -> Home Assistant webhook
├── mlgw-emu/              # MasterLink Gateway emulation
├── web/                   # web UI (mdt-web)
├── mcu-firmware/
└── .pymcuprog-venv/        # hidden venv for the UPDI flasher
```

Systemd units land in `/etc/systemd/system/`; the bridge config lives
at `/etc/ml-source-bridge.toml`; the shairport D-Bus policy lives at
`/etc/dbus-1/system.d/shairport-sync-instance-policy.conf`. The
`dl-docs/`, `docs/` and `home-assistant/` live in the repo only — they
aren't installed onto the Pi.

## License

CC BY-NC-SA 4.0 — non-commercial use only, derivatives must use the
same license. See [LICENSE](LICENSE).
