# TB version of mdtv2-tools
This is a clone from Philip Voigt's great Masterlink Toolset MDTV2 (here's the gitlab repo: https://gitlab.com/masterdatatool/software/mdtv2-tools)
I have started to experiment with a sendspin provider which this repo contains in addition to all of Philip's stuff. Still in experimental state. Also this repo contains changes to allow more than one streaming protocol per B&O source (e.g. sendspin and airplay for N.MUSIC)

# Attention
The installer is still Philip's original installer. When installing this repo you need to manually install and configure sendspin on the pi. Changing Philip's installer is still WIP.

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
| **DL'86 music-system control** | Remote control your DL enabled BeoCenter or BeoMaster turntable | `dl-docs/dl86-music-system/`, `dl-scripts/dl86-system/` |
| **Phono capture (turntable)** | Sample script to do a 60 s record from a turntable into FLAC: triggers `BG.Play`, configures the ADC, records and optionally applies a software RIAA de-emphasis. | `dl-scripts/dl80-turntable/` |
| **CD capture (music system)** | Sample script to do a 60 s record from a CD source: triggers DL'86 CD on, configures ADC, records, encodes FLAC and sends standby. | `dl-scripts/dl86-system/` |
| **MasterLink protocol debugger** | Pretty-prints every ML telegram on the bus, decoded (TO/FROM/PT/payload). Compact and verbose modes. | `ml-debug/` |
| **Datalink protocol debugger** | Pretty-prints every DL'80 and DL'86 message, decoded (opcode names for DL'80; address/format/payload breakdown for DL'86 including status frames with volume/track/standby semantics). | `dl-debug/` |
| **Bus state variables** | Daemon that keeps the current ML / DL'80 / DL'86 status (active source, transport, track, volume) in Redis keys `state:ml` / `state:dl80` / `state:dl86`, and publishes a change event on `link:<bus>:state`. Read with one `redis-cli GET`. | `state-tracker/` |
| **ML device discovery** | The same daemon sweeps the MasterLink bus with `MASTER_PRESENT` pings and keeps a live inventory of which addresses/devices are present (AM / VM / SC / link nodes, with class) in `state:ml:devices`. Runs at startup and on demand (`PUBLISH link:ml:discover`). Non-disruptive — doesn't interrupt playback. | `state-tracker/` |
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
curl -sSL https://gitlab.com/masterdatatool/software/mdtv2-tools/-/raw/master/bootstrap.sh | sudo bash
```

That installs git, clones the repo into `/opt/mdt-tools-src/`, then
runs `install.sh` which handles everything else (apt deps, boot config
patch for the DAC+ADC overlay + UART, systemd units, hidden pymcuprog
venv). The installer may say `REBOOT REQUIRED` if it had to change
`/boot/firmware/config.txt` — if so, `sudo reboot`.

If you'd rather inspect the script before piping it to bash:

```sh
curl -sSL https://gitlab.com/masterdatatool/software/mdtv2-tools/-/raw/master/bootstrap.sh
```

Or the manual two-step:

```sh
git clone https://gitlab.com/masterdatatool/software/mdtv2-tools.git
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

## Configuration cheatsheet

The bridge reads `/etc/ml-source-bridge.toml`. Common sections:

```toml
# Role we play on the bus
role = "sc"           # or "am"

# Audio sources we claim (one or more)
[[sources]]
source_byte  = 0xA1   # N.RADIO
provider     = "airplay"
# or, for multiple providers 
# provider = ["sendspin", "airplay"]
# provider_default = "sendspin"
display_name = "N.RADIO"

# when using multiple providers for one Source set a name for each provider
# ---- display per provider  --------------------------------------------------
# [provider_displays]
# airplay  = "Apple Music"
# sendspin = "Music Assistant"

# Beo4 LIGHT key → arbitrary shell commands (home automation hook)
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

See `ml-source-bridge/config.toml.example` (also copied to
`/etc/ml-source-bridge.toml` on first install) for the full set of
options and the complete Beo4 key name table.

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
├── mcu-firmware/
└── .pymcuprog-venv/        # hidden venv for the UPDI flasher
```

Systemd units land in `/etc/systemd/system/`; the bridge config lives
at `/etc/ml-source-bridge.toml`; the shairport D-Bus policy lives at
`/etc/dbus-1/system.d/shairport-sync-instance-policy.conf`. The
`dl-docs/` reference material lives in the repo only — it isn't
installed onto the Pi.

## License

CC BY-NC-SA 4.0 — non-commercial use only, derivatives must use the
same license. See [LICENSE](LICENSE).
