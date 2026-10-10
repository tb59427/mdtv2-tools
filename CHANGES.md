# Changes in this fork

This repository is a fork of **mdtv2-tools by Philip Voigt**
(<https://gitlab.com/masterdatatool/software/mdtv2-tools>), licensed under
[CC BY-NC-SA 4.0](LICENSE). As the license requires, this file lists what was
changed. The fork is based on the original's `master` at commit `2cb5118`
("ml-source-bridge: DL'80 turntable as an ML source"). Everything not listed
here is Philip's work, unchanged.

## Included from upstream, not yet in its master

- **Wake only when needed** (Philip Voigt, `cb1b8c0`, from upstream branch
  `fix/wake-only-when-needed`, which upstream keeps "pending verification on
  real hardware"):
  - auto-wake is skipped when the bus is already on our source (own
    bookkeeping, cross-checked against the state-tracker's `state:ml`);
  - new `wake_target` setting (`vm` / `am` / `off` / an ML address);
  - the SC's distribution grant echoes the requested source byte instead of
    a hard-coded 0xA1;
  - fix: the first DIST_REQUEST after start could be dropped by the dedup.

  Touches `roles/source_center.py`, `core/builders.py`, `ml_source_bridge.py`,
  bridge README and config example.

## Added in this fork

### New providers

- **Sendspin** (`providers/sendspin.py`): Open Home Foundation multi-room
  audio, e.g. from Music Assistant. Talks to the `sendspin daemon` over MPRIS
  on the `sendspin` user's session bus, via a narrow sudo rule
  (`mdt ALL=(sendspin) NOPASSWD:SETENV: /usr/bin/dbus-send`).
- **MPD** (`providers/mpd.py`): Music Player Daemon over plain TCP (port
  6600); optional `[mpd]` config table.
- Setup of both: [docs/providers.md](docs/providers.md).

### Several providers per ML source

- `provider` may be a list, e.g. `["sendspin", "airplay"]`
  (`providers/multi.py`, `MultiSourceProvider`). Last writer wins: a provider
  that starts playing takes the source and the previous one is paused.
  `provider_default` picks who gets a Beo4 PLAY from idle.
- `[provider_displays]` gives each provider its own label on the B&O
  display (e.g. "Apple Music", "Music Assistant").
- AirPlay `is_playing` no longer falls back to the ALSA PCM state, which is
  RUNNING whenever *any* client plays through dmix.
- Requires all providers to play through ALSA `dmix`; shairport-sync is
  configured with `output_device = "plug:dmix"` (see docs/providers.md).

### Now playing on redis

- The bridge publishes per source which provider plays and what --
  `state:nowplaying` (hash) and `link:ml:nowplaying` (pub/sub) -- for both
  roles and independent of `auto_wake` (`core/nowplaying.py`).
- Providers distinguish `playing` / `paused` / `idle` (`playback_state`) and
  report cover art (`art_url`, MPRIS `mpris:artUrl`).

### Music recognition for the turntable

- Optional (`[turntable] recognize = true`): identifies what's on the record
  via Shazam (shazamio, unofficial; own venv installed by `install.sh` only
  when enabled) and shows title / artist / album / cover. Audio-only track
  detection (silent gaps), results shown only when two attempts agree.
  `providers/audio_tap.py` (pass-through tap in the loopback),
  `providers/phono_recognize.py`. Optional test snippets via `tap_dir`
  (meant for a tmpfs).

### Music recognition on the bus

- Optional (`[ml_listen] enabled = true`, `core/ml_listen.py`): the HAT's ADC
  hears the MasterLink audio lines (PCM1862 VIN1/VIN2, measured). While the
  audio master plays a source the bridge doesn't provide (CD, A.MEM, ...),
  the bridge records from VIN1 and identifies the music with the same
  recognizer; results go to `state:nowplaying` / `link:ml:nowplaying` keyed
  by the bus source, with `"origin": "ml_listen"` and the track number. A
  new track number from the audio master starts a new recognition.
- The ADC is shared with the turntable, which has priority
  (`core/adc.py`); the turntable provider claims it before its loopback.
- `audio_tap.py`: `--channel` for the result channel, `SIGUSR1` = new track.
- Original album from MusicBrainz (`core/musicbrainz.py`, `album_lookup`):
  Shazam often names a compilation; the listener looks up the release that
  is a plain Album, preferring the one with the track at the CD's track
  number, and takes its Cover Art Archive cover. `phono_recognize.describe`
  passes Shazam's ISRC on.
- After 20 s of silence the listener reports the source idle (and playing
  again when the music returns): the state tracker misses a stop caused by
  a source-less RELEASE.
- ha-notifier: `"ml_listen"` in `sources` forwards all of them. HA:
  `sensor.mdt_ml_listen` in `mdt_webhook.yaml`; the button-card shows bus
  sources too (`bus_image`). Web UI: switch and source filter on the
  Turntable tab. `install.sh` sets up the shazamio venv for it as well.

### Home Assistant

- **ha-notifier** (`ha-notifier/`, new service): forwards now-playing
  changes to a Home Assistant webhook -- debounced, deduplicated, keepalive,
  retries -- and serves AirPlay cover art (shairport-sync's cover cache) on
  port 8099. Configured in `[ha_notifier]`.
- **HA configuration** (`home-assistant/`): automations, scripts, template
  sensors and a button-card for N.MUSIC, including streams that don't come
  from Music Assistant. Guide: [docs/home-assistant.md](docs/home-assistant.md).

### Web UI

- `web/mdt_web.py` (new service `mdt-web`, port 80, Digest login, opt-in
  via `[web]`): status, sources and providers, turntable, HA notifier,
  LIGHT keys, MLGW emulation. Edits `/etc/ml-source-bridge.toml` with
  tomlkit (only changed values; comments and unknown settings kept), after
  the bridge validated it with the new `ml_source_bridge.py --check-config`;
  keeps backups. Guide: [docs/web-ui.md](docs/web-ui.md).
- Services restart on request over redis `link:ctl:restart`, no root:
  the bridge exits (Restart=always), ha-notifier exits with code 3
  (Restart=on-failure) and idles while disabled instead of exiting.

### MasterLink Gateway emulation

- `mlgw-emu/` (new service `mlgw-emu`, opt-in via `[mlgw]`): the Pi answers
  Home Assistant's mlgw integration like a B&O MasterLink Gateway -- MLGW
  protocol on port 9000 (login, Beo4 commands by MLN onto the bus), telnet
  `_MLLOG` on port 23 (every ML telegram), device list via mdt-web, and the
  MLGW's events derived from the bus (LIGHT/CONTROL, all standby, source and
  picture & sound status), verified side by side against a real MLGW.
  Devices with their ML bus addresses are edited in the web UI (import of a
  real MLGW's export). Works around the integration's address-learning race.
  Guide: [docs/mlgw-emulation.md](docs/mlgw-emulation.md).
- `ml-debug`: picture format (SOURCE STATUS) and the PICT/SOUND STATUS
  fields were decoded one byte off; fixed.

### Install and security

- `install.sh` deploys and enables `ha-notifier`; installs the Sendspin sudo
  rule when a `sendspin` user exists and removes an older rule that allowed
  `/usr/bin/sh` as that user.
- `/etc/ml-source-bridge.toml` is now `root:mdt 0660` (it can hold the
  webhook URL and tokens; group-writable for the web UI); the bridge unit
  gets `SupplementaryGroups=mdt` to keep reading it.
- `install.sh` also deploys and enables `mlgw-emu` and `mdt-web` (both idle
  until enabled in the config) and installs `python3-tomlkit`.
- `bootstrap.sh` installs from this fork by default and switches an existing
  clone's `origin` over (set `REPO_URL` for the original).

### Documentation

- [docs/providers.md](docs/providers.md) -- AirPlay, Sendspin, MPD, ALSA.
- [docs/home-assistant.md](docs/home-assistant.md) -- HA setup.
- [docs/web-ui.md](docs/web-ui.md) -- the web UI.
- [docs/mlgw-emulation.md](docs/mlgw-emulation.md) -- replacing a B&O
  MasterLink Gateway for Home Assistant.
- [docs/testing.md](docs/testing.md) -- running a checkout as a test
  instance next to the installed version.
- README, bridge README and `config.toml.example` updated accordingly.
