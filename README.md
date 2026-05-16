# mdt-tools

Raspberry Pi tooling for talking to a Bang & Olufsen MasterLink (ML) bus
through an ATtiny826 HAT. Lets a Pi present itself to a B&O system as a
Source Center (SC) and bridge a modern audio source (e.g. AirPlay) onto
the bus, so it shows up as N.RADIO / N.MUSIC on a real BeoCenter / BeoSystem.

## What's in here

| Path | What it is |
|---|---|
| `broker/` | `mdtv2-broker.py` -- ATtiny826 ↔ Redis bridge over `/dev/serial0`. Frames CHAN_ML / CHAN_DL86 / CHAN_DL80 / CHAN_GPIO / CHAN_PING bytes between the MCU's wire protocol and Redis pub/sub channels. |
| `ml-source-bridge/` | `ml_source_bridge.py` -- subscribes to ML telegrams via Redis, plays a chosen role (SC / AM), routes audio in from a provider (currently AirPlay via shairport-sync). |
| `ml-debug/` | `ml_debug.py` -- pretty-prints every ML telegram on the bus. User-launched, no service. |
| `mcu-firmware/` | Pre-built ATtiny826 firmware `.hex` + `flash.sh` UPDI flasher. Source lives in a separate repo. |
| `system-config/` | RPi `config.txt` snippet that disables HDMI audio, enables HiFiBerry DAC+ADC, frees `/dev/serial0`. Applied by `install.sh`. |
| `install.sh` | One-command Pi setup. |

## Hardware

* Raspberry Pi 4 / 5 (Pi Zero 2W also fine) running Raspberry Pi OS Bookworm.
* ATtiny826 HAT with ML transceiver (this project's custom board).
* HiFiBerry DAC+ADC HAT for audio in/out.

## Quick start

On a fresh Raspberry Pi OS Lite (Bookworm or later), one line:

```sh
curl -sSL https://gitlab.com/masterdatatool/software/mdtv2-tools/-/raw/master/bootstrap.sh | sudo bash
```

That installs git, clones the repo into `/opt/mdt-tools-src/`, then runs
`install.sh` which handles everything else (apt deps, boot config patch
for HiFiBerry + UART, systemd units, hidden pymcuprog venv). The
installer may say `REBOOT REQUIRED` if it had to change `/boot/firmware/config.txt`
— if so, `sudo reboot`.

If you'd rather see the script before piping it to bash:

```sh
curl -sSL https://gitlab.com/masterdatatool/software/mdtv2-tools/-/raw/master/bootstrap.sh
```

Or the manual two-step:

```sh
git clone https://gitlab.com/masterdatatool/software/mdtv2-tools.git
cd mdtv2-tools
sudo ./install.sh
```

After the install (and reboot, if needed):

```sh
# 1. configure the bridge
sudo $EDITOR /etc/ml-source-bridge.toml

# 2. enable the services
sudo systemctl enable --now mdtv2-broker.service ml-source-bridge.service

# 3. follow logs
sudo journalctl -u mdtv2-broker.service -u ml-source-bridge.service -f

# 4. flash the MCU (optional if already programmed)
sudo /opt/mdt-tools/mcu-firmware/flash.sh \
     /opt/mdt-tools/mcu-firmware/firmware-v1.5.5.hex

# 5. inspect raw bus traffic (interactive, run as needed)
sudo python3 /opt/mdt-tools/ml-debug/ml_debug.py
```

## Repo layout vs. installed layout

`install.sh` copies code into `/opt/mdt-tools/`, mirroring the repo layout:

```
/opt/mdt-tools/
├── broker/
├── ml-source-bridge/
├── ml-debug/
├── mcu-firmware/
└── .pymcuprog-venv/        # hidden venv for the UPDI flasher; user never touches
```

Systemd units land in `/etc/systemd/system/`; the bridge config lives at
`/etc/ml-source-bridge.toml`; the shairport D-Bus policy lives at
`/etc/dbus-1/system.d/shairport-sync-instance-policy.conf`.

## License

CC BY-NC-SA 4.0 -- non-commercial use only, derivatives must use the same
license. See [LICENSE](LICENSE).
