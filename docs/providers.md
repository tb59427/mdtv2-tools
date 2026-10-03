# Audio providers: AirPlay, Sendspin, MPD

`install.sh` sets up the bridge and AirPlay (shairport-sync). Sendspin and
MPD are optional and installed by hand -- this page covers all three, plus
the ALSA setup that lets them share the DAC.

Tested on Raspberry Pi OS / Debian 13 (trixie), aarch64, with shairport-sync
4.3.7, Sendspin 7.5.0 and MPD 0.24.4.

## How the providers share the DAC

Only one provider owns an ML source at a time. On a source with several
providers (`provider = ["airplay", "sendspin"]`) the last one to start
playing wins and the bridge pauses the other. During that handover both may
briefly have the audio device open, so **every provider must play through
ALSA's `dmix`**, never the bare hardware device:

| Provider | Setting | Value |
|---|---|---|
| shairport-sync | `alsa { output_device }` in `/etc/shairport-sync.conf` | `"plug:dmix"` |
| Sendspin | `--audio-device` (or `audio_device` in its settings) | `dmix` |
| MPD | `audio_output { device }` in `/etc/mpd.conf` | `"dmix"` |

No `/etc/asound.conf` is needed: ALSA's built-in `dmix` device works on the
first sound card, which is the MDT HAT's DAC once `install.sh` has disabled
the on-board audio. `dmix` runs at a fixed 48 kHz; `plug:` in front of it
converts AirPlay's 44.1 kHz. Check with `aplay -L | grep dmix`.

The ALSA default device is *not* shared on this card -- with no card-specific
ALSA config, `default` is plain `plughw`, i.e. exclusive. A provider left on
`default` makes the others fail with "device busy" while it plays.

The turntable provider is the exception: its ADC->DAC loopback opens the
hardware device directly (`playback_device` in `[turntable]`). Don't put it on
the same source as a dmix provider.

## AirPlay (shairport-sync)

Installed by `install.sh` from the Debian package, together with a D-Bus
policy (`/etc/dbus-1/system.d/shairport-sync-instance-policy.conf`) that
shairport-sync 4.3.x needs to register its D-Bus names.

One manual change -- route it through dmix. In `/etc/shairport-sync.conf`:

```
alsa =
{
	output_device = "plug:dmix";
};
```

```sh
sudo systemctl restart shairport-sync
```

Everything else can stay at the defaults of the Debian build, which includes
the D-Bus and MPRIS interfaces on the system bus (check:
`shairport-sync -V` lists `dbus-mpris`). The bridge uses them for transport
control, play state, metadata and cover art. Cover art lands in
`/tmp/shairport-sync/.cache/coverart` (the default `cover_art_cache_directory`)
and must be readable by the `mdt` user for ha-notifier to serve it.

## Sendspin

Sendspin is the Open Home Foundation's synchronized multi-room protocol;
Music Assistant can stream to it. The Pi
runs the `sendspin daemon` client as its own user. Its MPRIS interface lives
on that user's *session* bus, which the bridge reaches via a narrow sudo
rule.

**1. User, session bus, linger**

```sh
sudo apt install dbus-user-session
sudo useradd --system --create-home --shell /usr/sbin/nologin \
     --groups audio sendspin
sudo loginctl enable-linger sendspin      # session bus starts at boot
id -u sendspin                             # note the uid, e.g. 997
```

The user must be called `sendspin` -- the provider and the sudo rule use
that name.

**2. Install the client** (with [uv](https://docs.astral.sh/uv/), as that
user):

```sh
sudo -u sendspin -H sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
sudo -u sendspin -H /home/sendspin/.local/bin/uv tool install sendspin
sudo -u sendspin -H /home/sendspin/.local/bin/sendspin --version
```

Update later with `sudo -u sendspin -H /home/sendspin/.local/bin/uv tool upgrade sendspin`.

**3. systemd unit** `/etc/systemd/system/sendspin.service` -- replace `997`
with the uid from step 1 and pick a name (it shows up in Music Assistant):

```ini
[Unit]
Description=Sendspin Multi-Room Audio Client
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=sendspin
SupplementaryGroups=audio
Environment=XDG_RUNTIME_DIR=/run/user/997
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/997/bus
ExecStart=/home/sendspin/.local/bin/sendspin daemon --audio-device dmix --name "Masterlink Bridge"
Restart=on-failure
RestartSec=10
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
# sendspin keeps its settings here; "-" = don't fail if it doesn't exist yet
ReadWritePaths=-/home/sendspin/.config

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now sendspin
```

`DBUS_SESSION_BUS_ADDRESS` matters: without it sendspin can't register MPRIS
and the bridge never sees it play.

**4. sudo rule for the bridge.** Re-run `sudo ./install.sh` from your
checkout: once a `sendspin` user exists it installs
`/etc/sudoers.d/ml-source-bridge-sendspin-dbus`
([`ml-source-bridge/sudoers-sendspin`](../ml-source-bridge/sudoers-sendspin)):

```
mdt ALL=(sendspin) NOPASSWD:SETENV: /usr/bin/dbus-send
```

i.e. the bridge may run `dbus-send` as `sendspin` and nothing else. (Older
setups allowed `/usr/bin/sh` here; install.sh removes that rule.)

**5. Configure the source** in `/etc/ml-source-bridge.toml`, e.g. Sendspin
and AirPlay sharing N.MUSIC:

```toml
[provider_displays]
airplay  = "Apple Music"
sendspin = "Music Assistant"

[[sources]]
source_byte      = 0x7A            # N.MUSIC
provider         = ["sendspin", "airplay"]
provider_default = "sendspin"      # gets Beo4 PLAY when nothing is playing
display_name     = "N.MUSIC"
```

```sh
sudo systemctl restart ml-source-bridge
```

**Check:** play something from Music Assistant to the Sendspin player, then

```sh
sudo journalctl -u ml-source-bridge -f | grep -E "sendspin|multi"
redis-cli HGET state:nowplaying 0x7a
```

`[sendspin] latched onto D-Bus name …` and a `playing sendspin` now-playing
entry mean it works. `no MPRIS name matching …` at startup is normal while
nothing streams -- sendspin only registers MPRIS during a session.

## MPD

MPD plays its own library, playlists and web radio; the bridge controls it
over TCP (port 6600).

**1. Install**

```sh
sudo apt install mpd
```

The Debian package runs MPD as user `mpd` in group `audio`.

**2. Configure** `/etc/mpd.conf` -- the parts that matter:

```
music_directory     "/var/lib/mpd/music"
playlist_directory  "/var/lib/mpd/playlists"
bind_to_address     "any"          # "localhost" if no other MPD clients
zeroconf_enabled    "yes"
zeroconf_name       "Masterlink Bridge MPD"

audio_output {
    type        "alsa"
    name        "HiFiBerry (dmix)"
    device      "dmix"
    mixer_type  "none"             # volume is the B&O system's job
}
```

```sh
sudo systemctl enable --now mpd
sudo systemctl restart mpd
```

With `bind_to_address "any"` any MPD client on the LAN (MPDroid, Cantata,
…) can control it; the bridge itself only needs localhost.

**3. Configure the source** in `/etc/ml-source-bridge.toml`:

```toml
[[sources]]
source_byte  = 0xA1        # N.RADIO
provider     = "mpd"       # or part of a list: ["mpd", "airplay"]
display_name = "N.RADIO"

[mpd]                      # optional; these are the defaults
host     = "localhost"
port     = 6600
password = ""
```

```sh
sudo systemctl restart ml-source-bridge
```

**Check:** `mpc play` (from the `mpc` package), then
`redis-cli HGET state:nowplaying 0xa1`. MPD reports paused/playing/stopped
and title/artist/album; it has no cover art to pass on.
