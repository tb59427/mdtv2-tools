# Web UI (mdt-web)

Configure the Pi from a browser instead of editing
`/etc/ml-source-bridge.toml` over ssh.

## Enable

`install.sh` installs and enables `mdt-web.service`, but it stays idle until
the config has a `[web]` section with a password:

```toml
[web]
enabled  = true
port     = 80
username = "admin"
password = "<choose one>"
```

```sh
sudo systemctl restart mdt-web
```

Then open `http://<pi-hostname>.local/` (or the Pi's IP) and log in.

## Tabs

| Tab | What you set |
|---|---|
| **Status** | Services running, what each source is playing, MLGW emulation connections |
| **Sources** | Role (SC / AM), clock, auto-wake and wake target; sources with their providers — several per source for multi-stream, which one gets Beo4 PLAY — display names, provider names on the panel, MPD |
| **Turntable** | Loopback, panel text, ADC gain, deck STANDBY behaviour, music recognition -- for the record and for other bus sources (`[ml_listen]`, optionally limited to some sources) |
| **Home Assistant** | Now-playing webhook: URL, which sources (incl. "Bus sources (recognized)"), cover port |
| **LIGHT keys** | Beo4 LIGHT + key → shell command |
| **MasterLink Gateway** | MLGW emulation on/off, listen-only, login; rooms, devices, sources and favorites ([mlgw-emulation.md](mlgw-emulation.md)) |

Settings the UI doesn't show — ALSA devices, RIAA, DL'80 codes, redis — stay
as they are in the file.

## What Save does

1. Builds the new config and has the bridge validate it
   (`ml_source_bridge.py --check-config`) — exactly the checks it runs at
   startup: sources, providers, wake target, LIGHT keys. Problems are shown
   and nothing is written.
2. Keeps a copy of the current file in `/var/lib/mdt-web/backups/` (last 20).
3. Writes only the values that changed, with tomlkit: comments, layout and
   settings the UI doesn't know stay untouched. A key that isn't in the file
   is only added when it differs from the default.
4. Restarts only the affected services, via redis `link:ctl:restart` — no
   root needed: `ml-source-bridge` and `ha-notifier` exit and systemd starts
   them again, `mlgw-emu` reloads (devices) or restarts (`[mlgw]` settings).

Restarting the bridge interrupts playback for a moment.

Restore a backup:

```sh
sudo cp /var/lib/mdt-web/backups/ml-source-bridge-<time>.toml /etc/ml-source-bridge.toml
sudo systemctl restart ml-source-bridge ha-notifier mlgw-emu
```

## Security

- Plain HTTP with Digest login, meant for the home LAN.
- **Anyone who can log in can change the LIGHT-key commands, which run as the
  service user** — choose a good password, don't expose port 80 beyond the LAN.
- The service runs as `mdt` with only `CAP_NET_BIND_SERVICE` (for port 80).
  `install.sh` makes `/etc/ml-source-bridge.toml` `root:mdt 0660` so it can
  save it.

## Troubleshooting

```sh
sudo journalctl -u mdt-web -f
sudo -u mdt python3 /opt/mdt-tools/ml-source-bridge/ml_source_bridge.py \
     --config /etc/ml-source-bridge.toml --check-config
```
