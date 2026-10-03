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

### Home Assistant

- **ha-notifier** (`ha-notifier/`, new service): forwards now-playing
  changes to a Home Assistant webhook -- debounced, deduplicated, keepalive,
  retries -- and serves AirPlay cover art (shairport-sync's cover cache) on
  port 8099. Configured in `[ha_notifier]`.
- **HA configuration** (`home-assistant/`): automations, scripts, template
  sensors and a button-card for N.MUSIC, including streams that don't come
  from Music Assistant. Guide: [docs/home-assistant.md](docs/home-assistant.md).

### Install and security

- `install.sh` deploys and enables `ha-notifier`; installs the Sendspin sudo
  rule when a `sendspin` user exists and removes an older rule that allowed
  `/usr/bin/sh` as that user.
- `/etc/ml-source-bridge.toml` is now `root:mdt 0640` (it can hold the
  webhook URL and tokens); the bridge unit gets `SupplementaryGroups=mdt`
  to keep reading it.
- `bootstrap.sh` installs from this fork by default and switches an existing
  clone's `origin` over (set `REPO_URL` for the original).

### Documentation

- [docs/providers.md](docs/providers.md) -- AirPlay, Sendspin, MPD, ALSA.
- [docs/home-assistant.md](docs/home-assistant.md) -- HA setup.
- [docs/testing.md](docs/testing.md) -- running a checkout as a test
  instance next to the installed version.
- README, bridge README and `config.toml.example` updated accordingly.
