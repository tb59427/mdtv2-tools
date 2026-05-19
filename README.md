# mdt-tools

Raspberry-Pi-based interface to a Bang & Olufsen MasterLink (ML) and
Datalink ('80 / '86) audio system. Lets a small Pi + a custom HAT
present itself to an old B&O music system as a Source Center (SC) or
Audio Master (AM), control legacy Datalink components (Beogram
turntables, BeoMaster CD players), record their audio, and hook the
LIGHT key on Beo4 remotes into modern home automation.

The hardware side is an ATtiny826-based HAT with an ML transceiver
(custom board); the audio side reuses the kernel's
`hifiberry-dacplusadc` overlay, which our HAT happens to be pin-
compatible with. Tested daily on a BeoCenter 2 + BeoSystem 3 stack
and a Beogram 5500 turntable.

## Features at a glance

| Feature | What it does | Lives in |
|---|---|---|
| **AirPlay → MasterLink source** | Pi appears on the ML bus as an SC, claims a configurable source byte (e.g. N.RADIO), pipes shairport-sync audio onto the bus with track / artist metadata. | `ml-source-bridge/` (`role = "sc"`) |
| **Linkspeaker emulation** | Pi pretends to be an Audio Master so ML link-speakers in setups without a real BeoMaster can play sources we provide. | `ml-source-bridge/` (`role = "am"`) |
| **LIGHT-key home automation** | Beo4 remote `LIGHT + <any key>` runs a user-configured shell command (Home Assistant call, MQTT publish, GPIO toggle, whatever). Friendly names like `step_up`, `digit_1`, `red`, `play`. | `ml-source-bridge/` (`[light_handler]`) |
| **DL'80 turntable control** | Send single-byte DL'80 commands (`BG.Play`, `BG.ADV`, `Sys.Standby`, …) from `redis-cli`. Verified on a Beogram 5500. | `dl-docs/dl80-beogram/`, `dl-scripts/dl80-turntable/` |
| **DL'86 music-system control** | Send variable-bit-length DL'86 commands (CD on, source select, next, prev, standby) for B&O integrated music systems. | `dl-docs/dl86-music-system/`, `dl-scripts/dl86-system/` |
| **Phono capture (turntable)** | One-command 60 s record from a turntable into FLAC: triggers `BG.Play`, configures the on-HAT ADC for VIN4 + PGA, records, optionally applies a software RIAA HP-30 de-emphasis. | `dl-scripts/dl80-turntable/` |
| **CD capture (music system)** | One-command 60 s record from a CD source: triggers DL'86 CD on, configures ADC for VIN3 at 0 dB, records, encodes FLAC, sends standby. | `dl-scripts/dl86-system/` |
| **MasterLink protocol debugger** | Pretty-prints every ML telegram on the bus, decoded (TO/FROM/PT/payload). Compact and verbose modes. | `ml-debug/` |
| **Datalink protocol debugger** | Pretty-prints every DL'80 and DL'86 message, decoded (opcode names for DL'80; address/format/payload breakdown for DL'86 including status frames with volume/track/standby semantics). | `dl-debug/` |
| **One-line install** | `curl … bootstrap.sh \| sudo bash` on a fresh Pi OS Lite installs apt deps, patches `config.txt`, sets up systemd services, deploys all code, builds a hidden pymcuprog venv for flashing. | `bootstrap.sh`, `install.sh` |
| **UPDI flashing** | `sudo flash.sh firmware-vX.Y.Z.hex` toggles UART.SEL, stops the broker, flashes the MCU via pymcuprog, restarts the broker. No manual venv juggling. | `mcu-firmware/flash.sh` |

## Architecture

```
       Beo4 remote                   Beogram / CD player          AirPlay client
            │                              │                            │
            ▼                              ▼                            ▼
       MasterLink bus  ◄────────────►   Datalink bus            shairport-sync
            ▲                              ▲                            │
            │                              │                            │
       ╔════╧══════════════════════════════╧═════════════╗              │
       ║      ATtiny826 HAT  (ml / dl-80 / dl-86)        ║              │
       ║         ↕ /dev/serial0 + GPIO + I²S             ║              │
       ╚═════════════════════════╤═══════════════════════╝              │
                                 │                                      │
                                 ▼                                      │
                          mdtv2-broker          ─────► Redis pub/sub ◄──┘
                                                            │
                                                            ▼
                                    ml-source-bridge  /  dl-debug  /  ml-debug
                                    (Python services + tools)
```

`mdtv2-broker` is the only thing that talks to the MCU. Everything else
talks to Redis channels (`link:ml:*`, `link:dl80:*`, `link:dl86:*`,
`link:gpio:*`), so you can drop in extra subscribers without touching
the broker.

## Repository layout

| Path | Description |
|---|---|
| `broker/` | `mdtv2-broker.py` — ATtiny826 ↔ Redis bridge over `/dev/serial0`. Frames `CHAN_ML` / `CHAN_DL86` / `CHAN_DL80` / `CHAN_GPIO` / `CHAN_PING` bytes between the MCU's wire protocol and Redis pub/sub channels. |
| `ml-source-bridge/` | `ml_source_bridge.py` — main daemon. Plays SC or AM, runs source providers (AirPlay), and hosts the `[light_handler]` home-automation hook. Config: `/etc/ml-source-bridge.toml`. |
| `ml-debug/` | `ml_debug.py` — MasterLink protocol pretty-printer. User-launched. |
| `dl-debug/` | `dl_debug.py` — DL'80 + DL'86 protocol pretty-printer with status-payload decoding (track, volume, transport state, standby). |
| `dl-docs/` | Verified DL'80 (Beogram) and DL'86 (music-system) command tables. |
| `dl-scripts/dl80-turntable/` | Manual phono-capture scripts: triggers turntable transport over DL'80, records 60 s, encodes FLAC (with or without software RIAA). |
| `dl-scripts/dl86-system/` | Manual CD-capture script: triggers CD source over DL'86, records 60 s, encodes FLAC, sends standby. |
| `mcu-firmware/` | Pre-built ATtiny826 firmware `.hex` + `flash.sh` UPDI flasher. Source lives in a separate repo. |
| `system-config/` | Pi `config.txt` snippet that disables HDMI audio, loads the DAC+ADC driver, frees `/dev/serial0`. Applied by `install.sh`. |
| `install.sh` / `bootstrap.sh` | Pi installer (one-line via curl, or manual two-step). |

## Hardware

* Raspberry Pi 4 / 5 (Pi Zero 2W also fine) running Raspberry Pi OS Bookworm Lite.
* ATtiny826 HAT with ML transceiver — this project's custom board.
* DAC+ADC HAT (PCM5122 DAC + PCM1862 ADC over I²S; driven by the Linux
  `hifiberry-dacplusadc` overlay, which our HAT is electrically
  compatible with — we use the driver, the HAT itself is mdt's own).
* Audio-side wiring: turntable to VIN4 single-ended (phono); music-
  system tape-record-out to VIN3 single-ended (line-level CD capture).

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
sudo $EDITOR /etc/ml-source-bridge.toml

# 2. enable the services (install.sh already does this on first run)
sudo systemctl enable --now mdtv2-broker.service ml-source-bridge.service

# 3. follow logs
sudo journalctl -u mdtv2-broker.service -u ml-source-bridge.service -f

# 4. flash the MCU (optional; install.sh leaves the .hex in place)
sudo /opt/mdt-tools/mcu-firmware/flash.sh \
     /opt/mdt-tools/mcu-firmware/firmware-v1.5.5.hex

# 5. inspect bus traffic
sudo python3 /opt/mdt-tools/ml-debug/ml_debug.py        # MasterLink
python3 /opt/mdt-tools/dl-debug/dl_debug.py             # Datalink
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
display_name = "N.RADIO"

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
